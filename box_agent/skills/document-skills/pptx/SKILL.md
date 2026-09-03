---
name: pptx
description: Create, inspect, edit, validate, render, and QA presentation decks. Use when the user mentions PowerPoint, PPT, PPTX, HTML deck, slide deck, presentation, template slides, speaker notes, slide images, or asks to read, generate, create, make, design, or modify a presentation artifact. New decks default to controlled, editable HTML delivery; PPTX is an explicit optional export.
keywords: [ppt, pptx, slide, slides, deck, presentation, powerpoint, pitch deck, speaker notes, ppt制作, 做ppt, 可编辑ppt, 幻灯片, 演示文稿, 投影片, 演示, 宣讲, 汇报材料, 路演, 路演材料, 融资路演, 商业计划书, BP, 提案, 讲稿, 模板页, 路演ppt]
capabilities: [presentation.authoring]
related_skills: [html-templates]
---

# PPTX Skill

Use this skill whenever a presentation deck is an input, output, or deliverable.

### Theme preview intent (before deck authoring)

Treat an explicit request such as “有什么主题”, “先看看主题”, “让我选风格”,
“show me the themes”, or “theme options” as **theme discovery**, not as permission
to start authoring the deck. Before writing `outline.json`, scaffolding
`deck.json`, planning images, or researching content, render the representative
built-in gallery:

```bash
cd "${BOX_AGENT_OUTPUT_DIR:-.}" && \
${BOX_AGENT_NODE:-node} scripts/render_theme_gallery.js \
  --out theme-previews/index.html
```

Show or link `theme-previews/index.html`, say that every card is rendered by the
real controlled compiler, and ask the user to reply with the displayed
`theme_id` (or describe a different desired mood). Stop that turn after the
choice prompt. The next reply resumes the normal new-deck flow with that exact
registered theme; do not regenerate the gallery or create a second deck. Use
`--all` only when the user asks to see the complete catalog, and `--themes
id,id,...` for a requested shortlist.

This route is opt-in and must not slow the default path. A normal “帮我做 PPT”
request without theme-discovery intent still auto-matches a registered theme and
continues without asking the user to choose. If the user says “你选”, “随便”, or
already names a registered theme, skip the gallery and continue. A preview
gallery is disposable discovery output, not durable deck-authoring state, so the new-deck
hard start below has not begun yet.

For normal authoring, use the hybrid theme route after `outline.json` is valid.
First run `inspect_deck_contract.js --rank-themes` with the title, bound outline,
and source facts. This is discovery, not the new-deck scaffold: it returns a
deterministic shortlist of 5–8 registered themes with scores, matched signals,
hard conflicts, and composition metadata. Choose exactly one eligible candidate
by comparing the audience, content job, visual tone, density, and composition;
do not choose outside the returned candidates. Then scaffold with `--theme auto
--theme-model-choice <CANDIDATE_ID> --theme-model-reason <SHORT_REASON>`. Keep
the reason under 240 characters and state only a concise, user-visible fit such
as “创意机构场景与多彩海报语法更匹配”; do not include hidden reasoning.

The CLI remains authoritative. It rejects candidates with explicit hard
conflicts, candidates outside the shortlist, and large overrides of protected
subject rules, then preserves the deterministic choice under
`model_choice_rejected`. A valid rerank is recorded as `model_reranked`, not as
a user choice. The deterministic layer scores explicit keyword rules,
industry-fit metadata, mood metadata, light/dark intent, formality, and opt-outs
such as “不要拼贴” or “不要复古手绘”. Pass `--theme
<REGISTERED_THEME_ID> --lock-theme` only when the user explicitly named or
selected that exact theme; explicit locks skip shortlist/rerank. Do not turn the
fallback `blue-professional` id into an artificial explicit choice.
Explicit palette wording is persisted separately in `design_contract.palette`
and overrides theme color tokens while retaining the selected theme's
typography, shape, surface grammar, and allowed composition families. Preserve
explicit colors and their roles in `outline.design_requirements`; never reduce
“深蓝、米白、少量橙色点缀” to a generic cool or light theme match.
When a short brief names a subject with a stable, unmistakable visual identity,
the contract may add a sparse inferred subject palette after selecting the
registered theme—for example Tesla black/white/red, Forbidden City
vermilion/parchment/gold, or Minecraft forest/stone/grass green. This palette is
a soft semantic default only: any user-authored palette wording or exact hex
colors outrank it.
Explicit geometry, direction, relationship, and item-count requirements are
also persisted in `design_contract.slides` and are hard constraints. Use
`pyramid-hierarchy-v1` for an explicit pyramid instead of approximating it with
a technical diagram, and choose a layout whose declared capacity fits the
required number of visual items.
Explicit page roles also control the scaffold when no stronger geometry is
present: chapter dividers use `section-marker-v1`, a core conclusion with at
most three proof points uses `statement-focus-v1`, and an action-oriented close
with at most four actions uses `closing-next-steps-v1`. Matrix, chart,
architecture, process, and other explicit geometry outrank these role defaults.
If a statement or closing collection exceeds its layout capacity, preserve
every item by falling back to `cards-grid-v1`.

### Composition comparison intent

Theme discovery answers “which visual mood”; composition comparison answers
“which page grammar”. When the user asks to see every composition type, compare
families, or says several families look alike, render the standardized atlas:

```bash
cd "${BOX_AGENT_OUTPUT_DIR:-.}" && \
${BOX_AGENT_NODE:-node} scripts/render_composition_gallery.js \
  --out composition-previews/index.html
```

The atlas presents five user-facing directions, then renders the eleven internal
families and all three deterministic variants per family with matched content.
Users may choose a direction without knowing a family id; resolve that direction
to the best compatible family from the displayed content signals. Use the atlas
to compare hierarchy, module relationships, data regions, and page anchors
without mistaking a palette change for a new composition. This is also opt-in
discovery output and does not begin or alter durable deck-authoring state.

### New deck hard start

For a new deck, do not hand-create the top-level `deck.json` structure. After
the slide plan and ordered layout choices exist, the **first deck-authoring
command** must scaffold the complete ordered deck, including repeated layouts:

```bash
cd "${BOX_AGENT_OUTPUT_DIR:-.}" && ${BOX_AGENT_NODE:-node} scripts/inspect_deck_contract.js <LAYOUT_ID_FOR_SLIDE_1> <LAYOUT_ID_FOR_SLIDE_2> ... --theme auto --theme-model-choice <CANDIDATE_ID> --theme-model-reason "<SHORT_USER_VISIBLE_FIT>" [--family <ALLOWED_FAMILY_ID>] --image-mode <auto|creative_image_mode> --outline outline.json --title "<DECK_TITLE>" --fact "<VERBATIM_USER_FACT>" --research-fact "<FACT_FROM_COMPLETED_EXTERNAL_RESEARCH>" --assumption "<ONLY_IF_USER_EXPLICITLY_AUTHORIZES_IT>" --require-field <SLIDE_NUMBER>:<MANDATORY_FIELD> --out deck.json
```

Keep this scaffold invocation on one physical command line so its ordered
layout ids and flags remain unambiguous. The inspector must
also be the only shell command: do not append `tail`, `2>&1 | tail -N`, any other
pipe/redirection, or a second command. Registered layout ids and valid inspector
flags shown above may remain; `--outline` and `--out` are required, not the only
allowed inspector arguments.
Use `cd <artifact-root> && ${BOX_AGENT_NODE:-node}` on that same line;
do not split `cd` and the inspector across lines. Use the inspector path bundled
with this skill and preserve the exact one-line command form shown above.

The command writes the canonical skeleton, the required `image_plan` key in
`assets/generated/manifest.json`, and `qa/deck_contract.json`; it refuses to
overwrite an existing deck. `BOX_AGENT_OUTPUT_DIR` is their shared canonical
delivery root, so never add another `output/` prefix or create a sibling deck.
The scaffold imports public-research outline evidence into `research_facts`;
use `--fact`, `--research-fact`, and `--assumption` only for their declared
truth buckets. `--require-field` values must be exact ids from the selected
layout contract. Use this flag only for recoverable semantic content that the
user explicitly requires, such as chart data, table rows, KPI items, or project
metrics. Do not convert visual styling language such as color blocks, corner
labels, decorative badges, or rotated shapes into a mandatory `tags` field.
When outline-driven layout normalization removes a decorative field that the
outline never required as semantic content, the scaffold records a warning and
continues; substantive fields remain hard requirements. Use `auto` unless the brief explicitly activates
`creative_image_mode`. Ordinary `auto` is visual-first: default an eligible
cover to a generated visual anchor unless typography, data, diagrams, or another
editable structure is clearly stronger. For a multi-page ordinary deck, choose
image-capable layouts for meaningful context, problem, product, case, or vision
beats instead of letting every inner page collapse to text/cards; keep factual
accuracy-sensitive assets source-backed and keep structured information
editable. For an ordinary deck with four or more pages and no image-led inner
page, the scaffold promotes the first eligible qualitative cards page to
`image-feature-v1`; it excludes data, diagrams, explicit text-only pages, and
accuracy-sensitive real/source-backed media. Do not replace either JSON file
after scaffolding.
The selected theme contract exposes `composition.directions`,
`composition.default_family`, and `composition.allowed_families`. Directions are
the five user-facing choices; match their family ids to
`composition.families[].selection_signals` to decide which internal family best
fits the actual content. Use the default family
when there is no stronger signal. For product demos, image-led stories,
decision/data reviews, or technical architecture, resolve the chosen or inferred
direction to a compatible family and pass `--family`; never choose outside the
allowlist. The scaffold writes only that family plus a seed into the persisted
top-level `design` object; direction remains derived and cannot drift. Keep the
entire object unchanged for ordinary content patches, render, editor save, and
export. Only an explicit controlled redesign may replace it as described below.
Fresh scaffolds vary by default. Use `--design-seed` only for tests or when the
user explicitly needs a reproducible fresh composition.

After scaffold, `write_file(path="deck.json", ...)` and
`write_file(path="assets/generated/manifest.json", ...)` are forbidden. For each
`image_plan` entry whose decision is `generate`, call `generate_image` directly with
`watermark: false`, then run
`${BOX_AGENT_NODE:-node} <TRUSTED_SYNC_SCRIPT> assets/generated/manifest.json`
once. Do not reread those files when their current contents are already in context. For a routine
deck of roughly 12 slides or fewer, do not call
`plan_write` or `todo_write`; avoid micro-turns that only update coordination.
Use the loader-expanded absolute `scripts/sync_image_manifest_status.js` path
as `<TRUSTED_SYNC_SCRIPT>`; a relative, copied, or renamed script is rejected.

After authoring the slide content against the scaffold stdout, write one
`deck.patch.json` with the
exact envelope `{"slides":{"slide-01":{"props":{...}}}}`: every supplied
slide id is nested under the top-level `slides` object, never used as a
top-level key. The initial canonical patch write is one-shot whenever its body
fits in the current model response: call `write_file` without `chunk_index` or
`final`. Do not voluntarily start `chunk_index=0, final=false` merely because
the deck has many slides. Start ordered chunks only after an explicit
output-length/tool-argument recovery says the one-shot call did not execute.
Within the same active turn, continue an ordered write only when the preceding
`write_file` result explicitly reports the accepted path and next chunk index;
never guess an index. If the accepted prefix is already the complete JSON
document, finalize it with an empty next chunk and `final=true`. After a new
user turn or recovered Session, trust only durable files: if the canonical target
is absent or invalid, restart its write instead of trying to resume hidden
transaction state. Then run
`${BOX_AGENT_NODE:-node} scripts/apply_deck_patch.js deck.json deck.patch.json`.
The patch may change only `props`, `background`, and explicitly authorized
`truth_contract.assumptions`; it may not change ids, order, theme, layout,
truth mode, or scaffolded facts. Do not use `execute_code`, Python, or ad-hoc
shell rewrites for deck JSON. Truth report paths contain the exact slide id;
never reinterpret one as an array index.
Fields named `source` are short provenance captions only (for example a source
name or domain), never a page summary or the requested “需求—方案—价值” copy.
Put a page conclusion or client benefit in the layout's `insight`, `subtitle`,
or content collection. The patch normalizer may compact an overlong optional
source caption, so a source-caption length issue is not a reason to retry an
unchanged patch or block the whole deck.

For `chart-data-v1`, every included series must contain a real numeric value for
every included category. When the available facts do not form a complete shared
grid, keep the strongest complete factual category/series subset and move an
isolated metric into `highlights`, `insight`, or `subtitle`. Never pad a gap with
`0`, `—`, `待补充`, or an invented baseline/forecast. If no series has at least
two real values, use the scaffold's effective non-chart layout instead of
forcing a chart.

Treat “做一版汇报用的”, “换一种版式/构图”, “重新设计”, or a request for a
visually distinct version as a **controlled redesign**, not as a content-only
patch. Write one `deck.redesign.json` with optional top-level `design`
(`family`/`seed`), optional registered `theme_id`, and slide entries shaped as
`{"slides":{"slide-01":{"layout_id":"cover-editorial-v1","props":{...}}}}`,
then run
`${BOX_AGENT_NODE:-node} scripts/apply_deck_redesign.js deck.json deck.redesign.json`.
This command preserves slide ids/order, truth buckets, bound `outline_intent`,
and the previous layout's props in `layout_drafts`; it validates semantic
outline/layout fit plus theme/family compatibility before writing. Use it only when the user actually requests
recomposition. A normal wording/data correction still uses `deck.patch.json`.
Never simulate a redesign by changing only subtitles or by rewriting
`deck.json` directly.

The scaffold stdout is the complete selected-layout contract. Copy prop names
and shapes from `fields`/`editor.defaultProps`; do not guess aliases or inspect
the same layout again. Common arrays are `cards-grid-v1.items`,
`quadrant-matrix-v1.items`, `kpi-grid-v1.items`, `project-case-study-v1.metrics`, and
`timeline-horizontal-v1.steps`.

## 0. Non-negotiable Rules

1. New deck tasks use the controlled HTML compiler by default: `deck.json` → `layout_id + props` → `index.html`. Deliver HTML by default; create PPTX only when the user explicitly requests it. In a request that says to generate/make a PPT, wording such as “输出完整 PPT 内容方案” describes the content expected inside the finished deck; it is not an outline-only opt-out. Stop at an outline only when the user explicitly says “只要大纲/内容方案” or “不要生成页面”.
   When the host requests `plan_write` for a finished presentation, the plan must cover outline, scaffold/content/media, deterministic `index.html` finalization, and QA. Never publish an outline-only scope merely because the outline is the first durable artifact.
   Treat `outline_policy.pages[].expected_visual_item_count` as a hard authoring count for the selected layout collection (`items`, `steps`, `rows`, `tags`, or `actions`). Page-level “需求—方案—价值” copy belongs in the title/subtitle/closing framing when the visual itself requests a different number of nodes; do not replace a four-node process with three generic need/solution/value cards.
2. Existing PPTX/template edits preserve the original PPTX structure.
3. `python-pptx` must not create a new deck.
4. Do not silently downgrade generation mode.
5. Visual QA via rendering is optional, not required. See §4.2 for triggers.
6. If render is attempted but blocked (missing `soffice`/PDF renderer), continue without it; do not treat it as a delivery blocker.
7. `.slide` must be exactly `1920px × 1080px` (16:9). The controlled renderer owns this contract. On the legacy/custom-HTML route, copy `references/starter/common.css` to `drafts/common.css` and use its `.slide` block verbatim. `html_self_check.js` and `html_to_editable_pptx.js` hard-assert this exact size against every slide. **Do not pass `--width`/`--height`**; non-standard decks require the same explicit `--canvas WxH` on both scripts.
8. The controlled theme catalog bundled under `themes/` is the runtime source of truth and includes the curated executable Visual DNA set supported by this skill. Eleven built-in composition families and their variants are selected and persisted under `design`, so a machine without `html-templates` still has both theme grammar and structural variety. Use `html-templates` as an optional richer matcher when it is available, but never block or invent a theme when it is absent. See §3.0.
9. The controlled route authors content through the scaffold plus `deck.patch.json`; do not hand-write the full `deck.json` or compiled multi-slide HTML. `scripts/render_deck_html.js` is the only writer of controlled `index.html`. The fragment workflow in §3.5 applies only to an explicit legacy/custom-HTML escape route.
10. Any PPTX line geometry written by direct generation paths (`PptxGenJS` / OOXML / python-pptx / other direct generators, i.e., not `dom-to-pptx` HTML export) must avoid negative width/height. Normalize line geometry from start/end coordinates (`x1`,`y1`,`x2`,`y2`) into non-negative geometry before writing geometry boxes: `x=min(x1,x2)`, `y=min(y1,y2)`, `w=abs(x2-x1)`, `h=abs(y2-y1)`.
11. When the task declares `creative_image_mode`, successful image generation is mandatory for a complete delivery: at least one `generate_image` call must complete and the generated asset must be referenced in `assets/generated/manifest.json`. If `generate_image` is unavailable or every call fails, do not present the PPT as completed. When the deck remains structurally renderable, still finalize and deliver an editable degraded `index.html`, explain under the localized usage-note label from §6 that the required image was not generated, and preserve the manifest findings as warnings so the user receives a usable artifact.
12. New decks pass the content & outline gate before any HTML or image work (§1.1). A slide plan is the prerequisite for authoring slides. When the host supplies a `<presentation_config>` block, use each field's `source` to determine its strength: `explicit` values from the user's original request or an actively changed card choice are hard constraints, while `recommended` and `default` values are soft framing that may yield to the source material and original prompt. `confirmed_by=user` alone does not upgrade unchanged soft defaults. Do not ask again for choices already present in the block. For page count, `page_count_auto` and any range whose source is `recommended` or `default` are planning hints only: derive the natural count from independent narrative beats, content density, and any page structure the user supplied; never turn `5-10` into its midpoint or silently default to eight slides. Only a concrete page count or range explicitly stated by the user, including an actively selected range in the card, is hard. The host-facing option catalog lives in `references/presentation-preflight.json`; do not invent incompatible ids. A solution/design brief that names the goal, requested system components, emphasis, and approximate page count is sufficient to author a proposed architecture: keep `source_mode=user_provided`, frame unsupplied details as recommendations, and do not load `research-synthesis` by default. Route to `research-synthesis` only when the deck actually requires external evidence, market/company/policy facts, current data, citations, or source-backed quantitative claims. If two or more audience/scope/direction choices materially change the user-visible deck, call `request_user_decision`; do not present an A/B list only in Markdown. Internal recovery choices such as one-shot versus chunked `write_file` are implementation details: choose the safer path yourself. Do not use `request_user_input` for a missing fact. Search public facts within the selected research route's bounded budget; after that budget is exhausted, omit an optional unsupported claim or use an explicit placeholder such as `待补充`, `待客户确认`, or `暂无可验证公开数据` for a required field. Missing case metrics, quote amounts, contact names, or other user/private values are never pre-delivery blockers: use an explicit placeholder, finish the editable HTML, and summarize the gaps afterward. After a structured decision request, end the turn without deleting or rebuilding any existing outline, scaffold, manifest, or QA output. The user's next reply resumes this same deck from the Session Log and those durable files. (Hard `BLOCKED` is reserved for the `creative_image_mode` image rule above.)
13. Scaffold the full `deck.json` once, using only registered ids returned by the compact discovery commands and selected for the validated outline; never guess a theme/layout id. Invoke the inspector directly without a pipe, `tail`, redirection, or a second command. Its stdout is the complete contract for that scaffold and should be reused while it remains in context. After a blocking structural validation fails, patch only the paths named in the fresh report. The patch compiler deterministically reconciles every existing manifest asset to its declared slide id and `prop_path`; a full-slide `background` is always written at slide top level, never under `props`. Once a repair patch has been applied, the old blocking reports are stale: invoke `finalize_controlled_deck.js` once instead of separately rerunning validators, render, self-check, and runtime probe. Never apply two repair patches from the same report. If the same refreshed spec issue class recurs twice, stop automatic repair and re-read the existing contract once. Source/URL/private-fact findings never trigger an automatic repair loop: use a neutral formulation, replace a required unavailable fact with an explicit placeholder, or omit the unsupported optional claim, then finish the HTML and summarize the finding afterward. Preserve every outline-bound title exactly plus the page's content anchors. Quantitative anchors may be split naturally across KPI/chart labels and values; do not duplicate a full source sentence in every cell merely to satisfy binding. Choose a neutral, source-safe outline title before scaffolding and place any placeholder in its supporting field. A public-research deck must not expose visible `待补充` for an optional gap; use supported copy instead. Reserve visible placeholders for required unavailable facts. Never rewrite scaffolded fact buckets, regenerate the whole deck, or reset with `--force` to clear validation errors.
14. Source-bound decks never invent named clients, projects, company/product names, financing rounds or stages, awards, publications, rankings, dates, team origins/sizes/history, project narratives, process steps, or future plans. User-authorized assumptions may support disclosed illustrative metrics or performance scenarios only; they do not authorize invented proper nouns, financing stages, dates, team facts, awards, or documentary claims. In a strict source-only request, factual fields such as statement/support copy, KPI detail, project positioning/caption, timeline step title/body, and card body must copy supplied wording, omit an optional field, or use an active-language missing-information marker only when a required field is genuinely missing; polished generic prose is still unsupported. Do not infer a funding round, infer a founding year from “third year”, invent a prior team size, or turn missing evidence into generic positive copy such as `复购率持续提升`. Creative image direction permits visual imagination, not factual invention. A generated project/case-study image must use `origin: "generated"` and an explicit concept/placeholder alt or caption. Before authoring, a deep-research deck consumes the workflow-supplied generic `presentation_handoff` contract. A `full` delivery creates the complete sourced deck, `partial` uses only its verified subset and marks or omits gaps, and `framework` creates an editable structure without external factual claims. `validate_outline.js --research-handoff ...` accepts only exact `verified_facts[].canonical` values in `entity | claim | source_type | source_url` form. It does not depend on the upstream validator name or research-skill status fields. Conflicting, unverified, cross-entity, or excerpt-unsupported research never enters `truth_contract.research_facts`. Run `validate_deck_truth.js` only as a post-generation source advisory after `index.html` exists. Its source, URL, or private-fact findings never block HTML delivery, invalidate an otherwise usable deck, or trigger a repair loop; summarize only concrete user impact under the localized usage-note label from §6.
15. Never create a fake bitmap with Pillow, SVG, a solid fill, or copied placeholder merely to satisfy a media field. If image generation fails, switch to a layout whose media is optional or retain the built-in editable placeholder and record the decision as `failed`/`skip`. In `creative_image_mode`, mark the image requirement blocked and the HTML degraded, but still deliver the structurally valid editable HTML. Never relabel a placeholder as `generated`, and never reuse one placeholder as several supposedly distinct generated images.
16. Exact user-authored `#RRGGBB` palettes are hard design contracts and outrank color-name paraphrases introduced by the outline. Two or three exact values map deterministically to background, primary, and accent unless the user labels those roles explicitly. Never replace an exact accent such as `#EF4444` with the nearest named red. A user instruction that forbids image generation is also a hard contract: every generated-image decision must remain `skip`, `generation_forbidden` is persisted in the manifest, and image QA must fail if a generated or required image entry reappears.

## 1. Route Decision

### New deck

Use this path by default:

For a broad project-review, sales-proposal, or operating-analysis request
without a supplied page order, query
`scripts/query_outline_recipes.js --text <SHORT_SCENARIO_LABEL>` before writing
the outline. Treat matched beats as a soft completeness checklist: omit, merge,
or split them according to the source material, and never override explicit
user order or page count.

1. **Pass the content & outline gate first (see §1.1 and `references/outline.md`).** Every new controlled deck writes one `outline.json` and validates it to `qa/outline_check.json` before theme/layout selection. When the user already supplied a complete page list, this is a lightweight traceability mapping, not a new invented storyline. A concise solution brief naming the intended system, integrations, processes, and page count is also enough to plan a proposal without external research. Do not write slide HTML or start image planning until that report is `ok`. Audience, purpose, style, page count, scope, and content direction are finite framing choices, not missing facts: never request them with `request_user_input`. If two or more such choices materially change the deck, call `request_user_decision` once with 2–6 options, put the recommended option first, set its id as `default_option_id`, request `requested_auto_submit_seconds: 30`, and declare `risk_level: "low"`, `reversible: true`, and `preserves_user_intent: true` only when those statements are accurate. Route to `research-synthesis` only when external evidence is necessary (§1.1). End the clarification turn after the tool call; the next user reply continues this deck rather than starting a fresh HTML task. In output mode, write the outline with the exact artifact-relative tool path `outline.json`; never use the absolute session-workspace path. Prefer one initial `write_file(path="outline.json", content=...)` call without chunk fields whenever the complete JSON fits in the current response. Start ordered chunks only after explicit output-length/tool-argument recovery. Within the same active turn, continue only from the immediately preceding `write_file` result. On a later turn or recovered Session, validate the durable target and restart the write if it is absent or invalid.
2. Inspect the built-in theme catalog before scaffolding when the durable deck files do not already contain a scaffold. For normal authoring, run `scripts/inspect_deck_contract.js --rank-themes --theme auto --outline outline.json --title "<DECK_TITLE>"` with the same source facts that will reach the scaffold, choose one eligible returned candidate, and preserve its short public fit reason. The shortlist is the model's complete authority: never invent an id, choose an ineligible candidate, or treat the deterministic recommendation as mandatory when another eligible candidate better fits the audience and visual job. Submit the choice through the hard-start command's `--theme-model-choice` and `--theme-model-reason`; inspect returned `theme_selection` because the CLI may reject it and safely retain the deterministic recommendation. If `html-templates` is available, its Visual DNA may inform the comparison, but it is not required. Use `--theme <REGISTERED_THEME_ID> --lock-theme` only after the user explicitly names or chooses that exact id; this bypasses model reranking. Use `default_theme_id` only when the matcher reports `fallback_default`; never copy it into the command merely because it is the default (see §3.0).
3. query layouts by page role/density/media needs, choose the **ordered layout id for every slide (including repeats)**, choose `--image-mode auto` or `creative_image_mode` from the brief, then run the hard-start scaffold command with `--outline outline.json --out deck.json` once. Semantic fidelity beats forced variety: a qualitative page must not use `chart-*` or `kpi-grid-v1` unless its outline evidence contains real quantities, and repeated layouts are allowed. Use `technical-diagram-v1` for professional architecture, system-integration, data-flow, and data-pipeline pages: set `diagram_kind`, author stable node ids plus explicit edges, and let the bundled DiagramSpec + ELK runtime compute the SVG. Keep `architecture-layered-v1` and `system-integration-v1` only for compatibility with existing decks. Use `quadrant-matrix-v1` for a true editable 2×2/四象限 priority matrix; do not approximate it with a table or generic cards. Use `dashboard-overview-v1` for a dashboard concept whose real values have not been supplied; use `kpi-grid-v1` only once quantitative evidence exists. A requested risk heatmap uses `heatmap-matrix-v1`; its editable semantic cells render as intensity levels without being mistaken for a plain table. A requested Gantt plan uses `table-data-v1` with `variant: "gantt"`; it supports a task column plus up to five editable phase columns and up to twelve work packages, and inactive cells use `"—"` rather than an empty string. The scaffold persists each page's title/message/layout/visual as `outline_intent`, normalizes strong visual mismatches (for example heatmap→heatmap matrix, 2×2 matrix→quadrant matrix, generic matrix→table, Gantt→Gantt table, architecture→technical diagram, integration→technical diagram, qualitative dashboard→dashboard overview), and safely converts an otherwise qualitative chart/KPI choice to cards instead of inventing values or requiring a model retry. Later QA checks explicit visual cardinality such as “三段式” or “四象限”. Do not discard or rewrite that intent metadata. Use `table-data-v1`, not `closing-next-steps-v1`, when a next-step page carries parallel fields such as task/action, role/owner, responsibility, member/name, status, or date; the closing layout is only for self-contained calls to action whose label and detail do not imply a responsibility matrix. The scaffold may enforce this distinction from the bound outline and reports any change under `layout_normalizations`; author only against the returned effective layout contract. For `source_mode=user_provided`, exact quantities in that page's message/bullets are evidence even when its external-link `evidence` array is empty; never downgrade a user-mandated editable chart or KPI page merely because the supplied facts do not need URLs. Use `project-case-study-v1` only for an actual source-backed project/case with proof metrics; it is not a generic image-plus-text layout for history, profiles, or other editorial narratives. A requested project case that combines a thumbnail/media region with project metrics remains `project-case-study-v1`; words such as “关键数字指标卡” describe the proof region and must not convert the whole page into `kpi-grid-v1`. Missing private project values may use visible `待补充` placeholders without changing that composition. Translate every explicit recoverable page-content requirement into an exact `--require-field SLIDE:FIELD` from that chosen contract: KPI grids use `items`, while project case studies use `metrics`. Decorative styling is not a recoverable content requirement: for example, a cover asking for a generated hero plus rotated corner labels uses the hero layout and composition styling, not `--require-field 1:tags`. A project page that must contain 2-3 metrics must use a layout whose fields include `metrics` (do not substitute `image-hero-split-v1`, which has no metrics), and a requested KPI page stays on `kpi-grid-v1` rather than being weakened to a generic cards layout. The scaffold stdout returns the complete selected-layout contracts and bound outline pages; do not call `inspect_layout.js` once per selected slide before or after scaffolding. Valid props are under `layouts[].fields` and examples/defaults under `layouts[].editor.defaultProps`. Do not query nonexistent `.props` / `.required_fields`, read the full manifest in chunks, use `execute_code` to inspect it, grep the registry source, or repeatedly inspect the same layout. Use `inspect_deck_contract.js --list-themes` and `query_layouts.js --list` for compact discovery.
   Use `image-feature-v1` for a wide image-led content page with editable narrative below it. Use `image-full-bleed-v1` when the brief explicitly asks for an entire-slide image, cinematic poster, campaign visual, or immersive vision page; its generated background, text-safe region, right-side visual-focus region, and dark wash are scaffolded as one recoverable layout contract. Do not approximate either request with `image-hero-split-v1` or a generic background on an unrelated layout.
4. plan slide-level image decisions in the scaffolded `assets/generated/manifest.json`. Derive one stable deck context/style anchor from the selected theme and repeat it inside every generated-image prompt; do not add a competing top-level manifest schema. For each declared slot choose `generate`, `use_existing`, or `skip` from the narrative job. In ordinary `auto`, generate an eligible cover by default and use optional fixed inner-page media slots unless the page is explicitly typography-, data-, diagram-, or editable-structure-led. In `creative_image_mode`, also promote an optional inner-page slot when that page's visual intent explicitly requests a generative bitmap medium such as a screenshot, photograph, poster, mockup, scene, or object study; do not generate images for ordinary text, cards, data, or diagram pages. A concrete subject normally uses a fixed-frame hero slot; atmosphere may use the slide-level background; typography/data-led pages may skip bitmap media. Prefer one dominant media treatment rather than filling both hero and background without a reason.
5. call `generate_image` for every `generate` item before final validation. Emit independent image calls together in one assistant tool-call batch; the executor runs this parallel-safe tool concurrently. Do not create a sub-agent merely to wait for image generation—the parent still needs the returned asset paths before manifest binding and QA. Always pass `watermark: false`. Localize every `use_existing` asset. Once the files exist, run `sync_image_manifest_status.js` once instead of manually editing the manifest. Store artifact-root-relative paths in the declared media prop or `slide.background`, and set media `origin` to `generated` or `asset`; the final `deck.json` never contains an unresolved generation request.
6. Fill the scaffolded `deck.json` with one controlled batch patch when practical, then run the single `scripts/finalize_controlled_deck.js deck.json --out index.html` command directly. The patch compiler first reconciles ready manifest assets to their exact slide/prop bindings. The finalizer treats deck structure and HTML rendering as hard prerequisites, records image-manifest findings as a delivery advisory, compiles HTML, runs HTML self-check and `probe_deck_runtime.js` at `1440x900`, then records source/truth findings as another non-blocking advisory. Once rendering succeeds, a downstream HTML/runtime QA failure is preserved as a degraded advisory and the existing `index.html` is still delivered; it must not start an automatic repair loop or be described as no output. On a blocking structural failure, patch only the named paths. Source/URL/private-fact findings are reported after generation and never start a repair loop. For public-research decks, do not expose `待补充` merely because a nonessential claim was not researched: omit that claim and use supported copy instead. For required unavailable public facts use `暂无可验证公开数据`; for required user/private facts use `待补充` or `待客户确认`, then continue without asking.
7. `finalize_controlled_deck.js` is the only normal finalization command. Do not hand-edit the compiled HTML, split finalization into separate successful validator/render calls, or add another `output/` prefix. The generated HTML is the default deliverable and includes the controlled editor runtime. A failed core deck schema or render step blocks delivery because no trustworthy HTML can be compiled. Exact outline title/message binding drift is semantic QA: preserve it as a degraded deck-spec warning and still render; do not enter another model repair loop solely for that drift. Image-manifest findings likewise do not suppress HTML; deliver the HTML as degraded and explain a missing explicitly required image under the localized usage-note label from §6. A failed HTML self-check, editor-fit, contrast, or export-geometry report keeps the already-rendered HTML as a degraded draft with the exact QA findings preserved; return that artifact and do not loop. A source/truth advisory with findings does not block or invalidate an existing `index.html`; keep the HTML and summarize only its concrete user impact afterward.
8. When the manifest contains `layout_contract`, run image layout contract validation after finalization.
9. export with `scripts/html_to_editable_pptx.js` only when the user explicitly requests PPTX, then run PPTX structural QA.
10. render and inspect when §4.2 triggers apply. Keep `deck.json` beside `index.html` as the reproducible generation source. After in-HTML edits, the saved HTML's embedded `#deck-document` is authoritative for that edited artifact; do not claim that the sibling `deck.json` was synchronized.

Read `references/controlled-layouts.md` for the contract, output bundle, legacy escape route, and editor boundary.

### 1.1 Content & outline gate (new decks)

The deck's page-level content is decided **before** layout and images. The skill
owns the *slide plan* (page → message → layout intent → visual), not deep
research. Materialize that plan as `outline.json` for every new controlled deck
and run `scripts/validate_outline.js outline.json --report qa/outline_check.json`
before scaffolding. Run this gate at the start of every new deck:

1. **Enough content already?** If the user (or an upstream expert/research step)
   already supplied page-by-page content — titles, order, key points, data —
   treat that as the source of truth, build/confirm the slide plan per
   `references/outline.md`, and continue. This is the common case; do not invent
   a new storyline over usable input.
2. **Solution/design brief?** If the user asks for a proposed architecture,
   system-integration plan, product concept, operating model, implementation
   approach, or process design and names the goal plus the components to cover,
   treat those requirements as sufficient `user_provided` input. Author a
   recommendation, not a claim about an existing deployed system. Do **not**
   load `research-synthesis` and do not browse by default. When a named external
   product/API assertion genuinely needs confirmation, use at most two targeted
   official-source lookups; if the deck expands into a sourced factual report,
   switch to branch 4 instead of accumulating ad-hoc searches.
3. **Only structure is unclear?** If the *facts* are available but two or more
   framing choices materially change the user-visible deck (audience, page count,
   ordering, what to emphasise, plan-vs-build), call `request_user_decision` once
   with one focused question and 2-6 concise options. Put the recommended option
   first. When it is low-risk, reversible, and preserves the original request,
   set it as `default_option_id` and request a 30-second automatic submission.
   End that turn after the tool call, preserve any existing artifacts, and resume
   from them after the reply. Do not silently guess a whole narrative, do not ask
   the user to choose an internal implementation detail, and do not repeat the
   options as Markdown.
4. **Topic needs facts you don't have?** If the deck needs evidence, market/
   industry/company/policy data, or claims that must be sourced, and the material
   is thin or absent, **do not fabricate and do not write a cold rejection.**
   Load the research workflow first:

   ```
   get_skill(skill_name="research-synthesis")
   ```

   Run the selected `research-synthesis` route for the facts the slide plan
   actually needs, preserve its dimension, cross-verification, and insight
   artifacts under `research/`, then build the plan and scaffold those
   statements with `--research-fact`. A one-line request for a sourced factual
   report still needs the full research handoff; a one-line solution/design
   brief belongs to branch 2 and is not "under-sourced" merely because it is
   concise.
   On a deep-research `public_authoritative_research` outline, use each page's
   `evidence` array as a verified fact ledger. Copy only exact canonical strings
   from the validated presentation handoff under `research/`; run
   `validate_outline.js` with that handoff's artifact-relative path passed to
   `--research-handoff`.
   Missing evidence, an unknown canonical value, or an entity mismatch is a hard
   research-to-outline handoff failure. If bounded research cannot pass that
   gate, do not use its prose: omit an optional claim or use
   `暂无可验证公开数据` for a required public fact, then continue with a source-safe
   outline. Remove decorative or structural numbers from narrative copy instead
   of laundering them into evidence.
   `AuthLevel` is a ranking hint, not proof: use a `site:`-constrained query for
   known first-party domains, discard SEO/mirror/unrelated results, and never
   call a source official unless the returned URL belongs to that institution.
   A search result establishes only a candidate URL; it does not verify a claim.
   For external public facts, use only evidence from a successful exact-page read
   or the fresh validated presentation handoff. User-provided material follows
   its separate trusted-input route. The validated handoff is the continuation path:
   a resumed deck may reuse its already-validated research URLs without repeating
   the broad search. If a `site:` query returns no matching host, do not invent the
   expected official URL: either read a known exact first-party URL successfully
   and use only its returned content, omit the optional claim, or use
   `暂无可验证公开数据` when the field itself is required.
   An ordinary request to make a factual deck already authorizes the normal use
   of public, authoritative sources needed to complete it. Do not ask the user
   for a second "permission to use public sources" after successful research.
   Respect a strict/private source boundary, but use a disclosed placeholder
   rather than pausing when the allowed sources do not contain a required fact.
   When authoritative sources conflict, state the conflict and use a neutral
   formulation instead of asking the user to resolve the evidence.
   Follow the selected research route's coarse-to-fine search budget. The PPT
   workflow must not replace it with a separate four-query cap. Inspect the full
   useful result returned by each search before narrowing. Cover distinct
   slide-relevant evidence gaps instead of lightly rephrasing an already-run
   entity/fact query. An empty authority-ranked or `site:` result does not by
   itself justify repeating the same intent without the filter. If an exact
   first-party URL is known, read it with an available exact-page tool. Prefer
   `web_extract`, or a user-selected MCP reader that accepts the exact URL and
   returns page body text. In officev3, `managed_browser_*` is also valid for
   independent public-web reads, and `user_browser_*` applies when research
   depends on the user's current page, login
   state, cookies, extensions, or intranet access; this does not require a
   separate authorization prompt. A URL-bound search summary may support a
   medium-confidence row when its exact excerpt is preserved with
   `evidence_basis=search_summary`; never bind it to a different result URL.
   Do not switch modes through `source_preference`.
   Persist durable
   findings in `research/` (relative to the presentation artifact root; the host
   stores it as `output/research/`), and stop only when the slide-relevant dimensions and
   conflicts are covered or the runtime's normal global search limit is reached.
   Before outline authoring, run the bundled research validator with
   `--report research/qa/{topic}_research_check.json` when the research route
   completed; a reduced sequential run should still cover distinct dimensions.
   Use a fresh delivery-allowed `full`, `partial`, or `framework` report when
   available, but if bounded research or its
   validator is unavailable or incomplete, continue with supported findings,
   omit optional claims, and use explicit placeholders for required facts.
   Cover, agenda, and section-divider pages are structural and may keep
   `evidence: []`; framework data pages still need an explicit unavailable-data
   placeholder when no verified fact exists.
   Removing numbers or rewriting a claim as qualitative prose does not verify it.
   On every non-structural framework page that keeps a required unsupported claim,
   the exact unavailable-data placeholder must appear in `message` or `bullets`; an empty
   `evidence` array by itself is not disclosure.
   Never delay `index.html` for a research/source gap. Search and direct browser
   evidence calls have their own bounded research allowance and do not spend the
   downstream deck-production budget. Once that report allows delivery, proceed
   directly to the outline: do not search again, reread `outline.md`, or update
   todos/plans. Read only the named research Markdown handoff once when its
   contents are absent from context, then write and validate `outline.json`.
   If outline validation fails, use the fresh issues in
   `qa/outline_check.json`, the current `outline.json`, and the validated research
   handoff to write one corrected `outline.json`; do not use shell/Python to
   bypass the controlled writer.
   If officev3 or the runtime restarts, a terse continuation recovers an
   incomplete controlled deck from the Session Log and these durable artifacts. A deck whose
   seven QA reports are already current and successful is not reopened.
   Do not re-open the same
   public pages through a browser merely to make already sufficient researched
   text pass `--fact`; that is exactly what `--research-fact` represents. If a
   browser backend is unavailable but search already supplied enough evidence,
   continue instead of retrying every URL.
   If `research-synthesis` is unavailable in this session, use the available
   search/browser tools for at most two targeted official-source attempts per
   unresolved slide-relevant claim and never repeat an equivalent failed
   search. If those tools are unavailable or do not return sufficient evidence,
   omit optional claims and use explicit placeholders for required facts; do
   not pause the deck or present unsourced content as fact.

Reserve `BLOCKED` for the `creative_image_mode` image-complete requirement (§0
rule 11), while still delivering structurally valid degraded HTML. A normal deck that is merely under-specified is handled by a
structural-choice question, bounded research, omission, or an explicit
placeholder, never by a flat refusal. Use assumptions only when the
user explicitly authorizes them. Record them in the slide plan, pass them via
`--assumption`, and visibly mark each affected slide `假设` or `示意`; never imply
fabricated data is sourced.

**`creative_image_mode` carve-out:** only after the user's brief has actually
activated `creative_image_mode`, a short topic (e.g.
"茉莉花茶制作过程") is a *creative brief* — expand it imaginatively into a visual
storyline; do **not** route it to `research-synthesis` or stall on questions
unless the user explicitly wants sourced facts/figures. Branch 4 above applies to
fact/evidence-driven decks, not creative/atmospheric ones. Do not use this
carve-out to convert an ordinary factual biography or sports story into creative
mode; `auto` can still generate its cover.

### `creative_image_mode`

Activate this mode for an explicit creative/image-rich request or an explicitly
visual pitch/launch/brand brief that asks for generated imagery; the literal
mode name is not required. A topic being visually interesting is not sufficient.
Its
authoritative decision, manifest, prompt, provenance, failure, and concurrency
rules live in `references/image-assets.md`. The short contract here is:

1. At least one real `generate_image` result—normally the cover—must be stored under `assets/generated/` and referenced by the manifest and deck.
2. Keep charts, tables, and process data editable; generated images provide hero, atmosphere, mockup, or background value rather than replacing recoverable data.
3. Full-slide/background images require a `layout_contract`; fixed-frame hero images use the inspected media slot.
4. If every required generation fails, report the mode as blocked. Do not satisfy it with CSS/SVG filler or a relabeled placeholder.

HTML delivery does not require the browser export host. If a requested PPTX export is blocked by browser-host preflight, deliver `index.html + deck.json` and report only the PPTX export as blocked, or switch to native `PptxGenJS` with the user's agreement.

### Existing deck or template

Use this path for edits:

1. copy original deck
2. extract text
3. apply edits
4. validate package
5. render and inspect only if §4.2 triggers apply

### Native `PptxGenJS`

Use only when the user clearly requires it:

1. native PowerPoint charts/tables are required
2. user requires PowerPoint-native structure
3. HTML-first is impossible and user accepts the tradeoff

Do not switch routes based on convenience.

## 2. Minimal Commands

| Task | Command |
|---|---|
| Preview representative themes | `${BOX_AGENT_NODE:-node} scripts/render_theme_gallery.js --out theme-previews/index.html` (`--all` only on request) |
| Compare every composition family | `${BOX_AGENT_NODE:-node} scripts/render_composition_gallery.js --out composition-previews/index.html` (11 families × 3 variants, matched content) |
| Validate outline | `${BOX_AGENT_NODE:-node} scripts/validate_outline.js outline.json --report qa/outline_check.json` |
| Rank theme candidates for model rerank | `${BOX_AGENT_NODE:-node} scripts/inspect_deck_contract.js --rank-themes --theme auto --outline outline.json --title "Deck title"` |
| Scaffold ordered deck + image manifest + inspect exact contract | `cd "${BOX_AGENT_OUTPUT_DIR:-.}" && ${BOX_AGENT_NODE:-node} scripts/inspect_deck_contract.js cover-hero-v1 cards-grid-v1 cards-grid-v1 --theme auto --theme-model-choice creative-mode --theme-model-reason "创意机构场景与多彩海报语法更匹配" --outline outline.json --title "Deck title" --out deck.json` |
| Query controlled layouts | `${BOX_AGENT_NODE:-node} scripts/query_layouts.js --role comparison --density medium-high --media-count 0` |
| Inspect a layout contract | `${BOX_AGENT_NODE:-node} scripts/inspect_layout.js comparison-two-column-v1` |
| Sync generated image statuses | `${BOX_AGENT_NODE:-node} <TRUSTED_SYNC_SCRIPT> assets/generated/manifest.json` (use the loader-expanded absolute script path from §0) |
| Apply one validated content patch | `${BOX_AGENT_NODE:-node} scripts/apply_deck_patch.js deck.json deck.patch.json` |
| Apply an explicit controlled redesign | `${BOX_AGENT_NODE:-node} scripts/apply_deck_redesign.js deck.json deck.redesign.json` |
| Validate deck spec | `${BOX_AGENT_NODE:-node} scripts/validate_deck_spec.js deck.json --report qa/deck_spec.json` |
| Record post-HTML source advisory | `${BOX_AGENT_NODE:-node} scripts/validate_deck_truth.js deck.json --report qa/truth_check.json` |
| Finalize controlled HTML (normal path) | `${BOX_AGENT_NODE:-node} scripts/finalize_controlled_deck.js deck.json --out index.html` |
| Render editable HTML | `${BOX_AGENT_NODE:-node} scripts/render_deck_html.js deck.json --out index.html` |
| Probe editor fit, contrast, and export geometry | `${BOX_AGENT_NODE:-node} scripts/probe_deck_runtime.js index.html --viewport 1440x900 --report qa/runtime_probe.json` |
| Check layout manifest | `${BOX_AGENT_NODE:-node} scripts/build_layout_manifest.js --check` |
| Extract text | `${BOX_AGENT_PYTHON:-python3} scripts/extract_text.py input.pptx` |
| Validate package | `${BOX_AGENT_PYTHON:-python3} scripts/validate_pptx_package.py input.pptx` |
| Render PPTX | `${BOX_AGENT_PYTHON:-python3} scripts/render_pptx.py input.pptx --out rendered` |
| Validate image manifest | `${BOX_AGENT_NODE:-node} scripts/validate_image_manifest.js assets/generated/manifest.json --deck deck.json --report qa/image_manifest.json` (add `--mode creative_image_mode --min-generated 1` only when that mode is active) |
| Validate image layout contract | `${BOX_AGENT_NODE:-node} scripts/validate_image_layout_contract.js index.html assets/generated/manifest.json --report qa/image_layout_contract.json` |
| HTML self-check | `${BOX_AGENT_NODE:-node} scripts/html_self_check.js index.html --dom-to-pptx --allow-local-images --report qa/html_self_check.json` ⚠️ 画布固定 1920×1080，不要追加 `--width/--height`（已被拒绝）；非标准尺寸用 `--canvas WxH` |
| Optional PPTX export | `${BOX_AGENT_NODE:-node} scripts/html_to_editable_pptx.js index.html output.pptx` ⚠️ 仅在用户明确要求 PPTX 时执行 |
| Check local deps | `${BOX_AGENT_PYTHON:-python3} scripts/setup_check.py` |
| Check HTML export env | `${BOX_AGENT_NODE:-node} scripts/check_html_export_env.js` |

Shell note: assign runtime variables in an earlier command/line before expanding
them. Do not write `PPTX_SKILL_DIR="..." "$PPTX_SKILL_DIR/scripts/..."`; the
shell expands `$PPTX_SKILL_DIR` before that inline assignment takes effect.

⚠️ **Dependency probing**: never use bare `node -e "require.resolve('playwright')"` to check for installed packages. Box-Agent installs Node deps into the **office-raccoon managed prefix** (`~/Library/Application Support/office-raccoon/node_modules/` on macOS, `$APPDATA/office-raccoon/node_modules/` on Windows, `~/.config/office-raccoon/node_modules/` on Linux), which is **not** on the default `NODE_PATH`. A naked `node -e` process will report every managed package as `not found`. Always use `scripts/check_html_export_env.js` (Node) or `scripts/setup_check.py` (Python) — both look in the managed prefix.

## 3. HTML-first Requirements

### 3.0 Before selecting a controlled theme (mandatory)

The `pptx` skill is self-contained. Its versioned `themes/*.json` catalog is
always authoritative and is exposed by `scripts/inspect_deck_contract.js` and
`layouts/manifest.json`. Select from those registered ids only. Each theme
includes selection signals, palette, typography, shape tokens, and finite
visual-style axes. The catalog provides the curated executable Visual DNA set
supported by this skill, so a machine without the separate `html-templates`
skill can still create the registered built-in style range.

When `html-templates` is available, invoke it with the original brief, deck
goal/audience, and intended density. Treat the returned Visual DNA
`template_id` as a matching hint, not an unchecked runtime id: resolve it to
the same registered base theme, or to an explicitly registered variant when
the brief calls for that variant. If a future matcher returns an id outside the
catalog, choose the closest built-in theme from its selection metadata and
report the visual limitation. The controlled renderer, not the model, applies
concrete CSS. Never copy Visual DNA fields into slide props and never improvise
CSS inside `deck.json`.

Pass an explicit density target in the args whenever the deck is a business,
product, launch, scenario-demo, consulting, board, investor, training, or
enterprise enablement deck. Default those decks to `Medium-High` unless the user
explicitly asks for sparse, manifesto, cinematic, or quote-led slides. This
keeps the style matcher from selecting an atmospheric low-density profile when
the page still needs working presentation substance.

If `html-templates` is unavailable, proceed directly from the built-in catalog.
This is a supported path, not a blocker and not automatically a limitation.
Use `default_theme_id` only when the brief does not clearly match another
registered theme.

Short entity-led briefs still carry visual meaning. The built-in matcher maps
fantasy or wizarding-world subjects to `vellum`, sandbox or voxel-game subjects
to `8-bit-orbit`, museum and cultural-heritage subjects to
`biennale-yellow`, and electric-mobility subjects to `neo-grid-bold`. Keep
these subject signals in the original brief passed to `--theme auto`; do not
replace a meaningful title such as Harry Potter, Minecraft, the Palace Museum,
or Tesla with a generic category before theme selection.

For a short one-sentence brief, classify the user before falling back to a
generic business theme. Audience outranks topic when they conflict: a primary
school solar-system lesson uses the child-friendly `daisy-days` system rather
than a formal science grid. User purpose and subject then select the closest
registered design language: classroom teaching uses `pin-and-paper`; science
history and space exploration use `cobalt-grid`; archaeology and ancient
civilizations use `stencil-tablet`; operating reviews use
`data-intelligence`; formal proposals use `consulting-navy`; food and
hospitality use `long-table`; youthful beauty launches use `capsule`; gentle
animation, travel, and wedding stories use `soft-editorial`; sports culture
uses `bold-poster`; portfolios use `block-frame`; public-interest campaigns
use `peoples-platform`; climate and nature research use `grove`; and
independent music uses `retro-zine`. These are broad intent classes, not a list
of isolated title exceptions. Preserve the user's original entity, audience,
and purpose together so layout-family inference can distinguish a data review
from a generic institutional report.

Ten high-frequency professional intents own dedicated visual systems rather
than sharing a generic business palette: employee onboarding, employee
handbooks, culture, and talent programs use `people-handbook`; investment
memos, valuation, earnings interpretation, capital allocation, and investor
relations use `capital-ledger`; clinical trials, cases, patient pathways, and
medical education use `clinical-atlas`; government, public policy, regulation,
municipal services, and public governance use `civic-brief`; thesis defenses,
research proposals, literature reviews, and research methodology use
`research-notebook`; manufacturing operations, production lines, lean quality,
and OEE use `factory-floor`; legal opinions, case analysis, disputes, evidence,
and compliance review use `legal-docket`; real-estate development, land
acquisition, site analysis, and asset facts use `property-atlas`; retail,
ecommerce, merchandising, GMV, SKU, and conversion analysis use
`commerce-pulse`; supply-chain, logistics, warehousing, inventory, fulfillment,
and OTIF use `logistics-control-tower`. Prefer these precise intent rules over broad industry
metadata. Keep a conflicting stronger audience rule intact—for example a
primary-school health lesson remains `daisy-days`.

For neo-brutalist block-frame briefs, keep palette intent explicit: select
`block-frame-mono-blue` when the user asks for high-contrast black/white with
only a restrained saturated-blue accent; keep `block-frame` for playful
multi-color block compositions. Do not collapse these into one fixed look.

For explicit comic-book, manga, storyboard, speech-bubble, halftone, or
漫画／分镜 briefs, select `comic-panel`. It provides real framed panels,
caption boxes, speech tails, action labels, and ink-offset shadows rather than
merely recoloring `block-frame`. On `technical-diagram-v1` pages, keep the
DiagramSpec SVG clean and professional: the comic language belongs to the page
header, caption, and outer panel, not to individual architecture nodes.

For explicit pixel-art, 8-bit/16-bit, arcade, CRT, retro-game, or 像素风／街机
briefs, select `8-bit-orbit`. Its dedicated renderer provides a deep-navy CRT
grid, neon pixel frames, stepped corners, status labels, and hard digital
shadows rather than relying only on generic square corners. On DiagramSpec
pages, apply the pixel language to the outer monitor frame and caption only;
keep the SVG nodes, labels, and edges professional and readable.

Use `technical-blueprint` for architecture, cloud infrastructure, platform
engineering, integration, runtime, and data-pipeline briefs. It defaults to
`technical-schematic` and supplies coordinate paper, specification rails, and
an engineering frame around clean DiagramSpec SVG output.

Use `product-console` for SaaS, software-product, AI-product, product-launch,
feature-demo, and product-interface briefs. It defaults to `product-showcase`
and supplies browser chrome, app-shell panels, status chips, and UI-first
product stages.

Use `data-intelligence` for KPI reviews, operating analysis, business
intelligence, analytics, finance, and decision-dashboard briefs. It defaults to
`analytical-exhibit` and supplies a high-density evidence console with KPI,
chart, table, and data-flow treatments.

`signal`, `soft-editorial`, `daisy-days`, `people-handbook`, `capital-ledger`,
`clinical-atlas`, `civic-brief`, `research-notebook`, `factory-floor`,
`legal-docket`, `property-atlas`, `commerce-pulse`, and
`logistics-control-tower` own dedicated CSS in
addition to their theme tokens. `signal` uses institutional navy/bone/gold
rules, serif authority, and ledger-like evidence surfaces. `soft-editorial`
uses warm paper, magazine rules, asymmetric reading rhythm, and softly colored
editorial blocks. `daisy-days` uses a recognizable pastel collage, chunky
outlined cards, dotted classroom rules, and a CSS flower/rainbow signature.
The professional systems remain distinguishable without bitmap media:
employee badge and pinned paper, valuation axes and financial hairlines,
clinical graph paper and specimen labels, policy docket and civic seal, or
monograph section marks and footnote rhythm; the five newer systems add safety
rails and QC tags, docket/exhibit rhythm, cadastral grids and north marks, SKU
labels and receipt barcodes, or container IDs and ETA routes. Their flagship
editable layouts are `factory-process-line-v1`, `legal-case-logic-v1`,
`property-factsheet-v1`, `commerce-funnel-v1`, and `supply-network-v1`.
Do not describe these themes as
palette-only variations.

### 3.1 Layout constraints

1. `.slide` must be exactly `1920px × 1080px`. The controlled renderer owns this geometry; copy `references/starter/common.css` only on the legacy/custom-HTML route (see §0 rule 7; `--width/--height` are rejected, use `--canvas WxH` only for an opt-in non-standard deck).
2. Leave 16-24px text slack to reduce PowerPoint wrap drift.
3. For top/middle/bottom layouts, center the main content group in the available middle area. Do not build slides by stacking blocks from the top with repeated `margin-top`; compute the content group's height and balance top/bottom whitespace with flex/grid alignment or explicit `top` values.
4. Do not leave a content slide as "short cards on the top half + decorative empty background" unless the slide is intentionally a divider, quote, cover, or cinematic pause. For normal business/product/demo/training pages, the primary content plus primary visual should occupy the main body area. If the supplied text is sparse, convert the remaining canvas into substance: generated image with a real narrative job, workflow arrows, before/after comparison, role swimlanes, demo steps, KPI strip, decision matrix, architecture/process schematic, or editable diagram. Prefer a specific visual grammar over repeated generic cards. Decorative glow, texture, or grid alone does not count as content.
5. Scenario, use-case, capability, and "four cards" pages need a second composition layer when each card only has a title and 1-2 short lines. Choose a registered layout that can express a flow, metric row, journey, storyboard, or matrix; use custom HTML/CSS/SVG only on the explicit legacy route.
6. Use relative asset paths.
7. Do not inline large images as data URLs.
8. Resolve every slide's image decision against its inspected slot/background contract. `references/image-assets.md` is authoritative for trigger, prompt, provenance, concurrency, manifest, and failure rules; do not duplicate or reinterpret them here.
9. ECharts/canvas charts are allowed only as HTML preview surfaces backed by `data-pptx-chart` and recoverable chart data. They must not be baked into `assets/bg-capture/*.png` or delivered as screenshot-only chart images when the data is available.
10. Keep page numbers on non-cover slides consistent with slide order.
11. Read `references/controlled-layouts.md`. Read `references/html-first.md` and `references/html-editable.md` only for the legacy/custom-HTML path or optional PPTX export internals.

### 3.2 Technical diagrams and DiagramSpec

Use the controlled `technical-diagram-v1` layout for architecture diagrams,
system-integration maps, and data pipelines. Its `nodes` and `edges` are the
recoverable DiagramSpec source; the bundled ELK runtime recalculates hierarchy,
spacing, and orthogonal routes. In the HTML editor, **Adjust** exposes node and
edge tables, add/delete controls, endpoint selection, and a **Re-layout** action.

Every technical diagram export root must follow this contract:

```html
<div data-pptx-diagram data-diagram-spec-src="assets/diagrams/slide-02.json">
  <svg><!-- DiagramSpec render result --></svg>
</div>
```

- Use either non-empty `data-diagram-spec` JSON or a portable local
  `data-diagram-spec-src` JSON file.
- Keep exactly one direct inline `<svg>` root. Never use `<img src="*.svg">`
  for a technical diagram; the image pipeline rasterizes that path.
- `bg_capture` treats the marked root and every descendant as non-decoration,
  so the graph cannot enter the slide background PNG.
- The exporter forces `svgAsVector: true` whenever any marked diagram exists
  and reports `diagramCount` plus `diagramVectorExport`.
- Phase 1 HTML capability: edit node text and edge endpoints/labels; add/delete
  nodes and edges; rerun automatic layout.
- Phase 1 PPTX capability: one SVG vector picture per technical diagram,
  scalable and eligible for PowerPoint's best-effort **Convert to Shape**.
  Do not promise node-level native PowerPoint editing.
- Pipeline node labels must name unique stages. Represent a feedback loop with
  an explicit return edge, not by adding a second node with the same label at
  the other end of the pipeline.

### 3.3 Data charts and ECharts previews

For data presentation slides, preserve data first:

1. When a slide contains quantities, rankings, comparisons, trends, proportions, KPIs, financials, market sizing, benchmark results, time-series data, or operational metrics, use a visible data display by default: native table, KPI strip, bar/line/area/pie chart, matrix, comparison table, or mini-dashboard. Two or more distinct normalized quantitative values on a data-heavy page make a concrete data-display intent mandatory at the outline gate; repeating the same metric in message, bullets, and evidence still counts once. Use plain bullets only when the data is too sparse or the user explicitly asks for text-only slides.
2. Prefer registered controlled layouts when their contracts fit: `chart-bar-v1` for three to seven simple categorical/ranking values; `chart-data-v1` for animated bar, column, line, area, pie, donut, or radar charts with two to twelve categories and up to four series; `heatmap-matrix-v1` for editable semantic risk/intensity matrices; `table-data-v1` for two to six columns and two to twelve rows, including its editable `gantt` variant; and `kpi-grid-v1` for headline metrics. For a business-progress / traction page backed by a single growth line or area series, keep `chart-data-v1`, set `presentation: "traction"`, and author two or three recoverable `highlights` (`value`, `label`, optional `note`) so the page leads with verified KPIs rather than a large empty chart card. `presentation: "auto"` recognizes common business-progress narratives, while `"standard"` preserves the general analysis treatment. Their `deck.json` props are the recoverable data source and the renderer emits the chart spec or editable cells; do not create a duplicate `assets/data` file for the same controlled data. Controlled charts bundle ECharts 6 locally, render with SVG, expose a registry-driven data grid, and export through the native PptxGenJS chart mapping.
3. For unsupported controlled shapes (scatter, bubble, combo, sankey, map, or larger tables), store data in `assets/data/*.json` and use the legacy/custom HTML data route. In `deck.html`, use ECharts for browser preview and layout tuning when the slide is chart-led or backed by `assets/data/*.json`. The chart root must be marked with `data-pptx-chart` and must reference or embed a chart spec via `data-chart-spec`, `data-chart-spec-src`, or a child `<script type="application/json" data-chart-spec>`. If a dataset exists in `assets/data/`, do not duplicate the numbers into static SVG, absolute-positioned bars, or text-only chart markup without linking the dataset.
4. When creating the final PPTX, convert available chart data to native PowerPoint charts/tables whenever the recipient may edit numbers. Do not flatten an ECharts canvas/SVG into a screenshot just because it looks correct in HTML.
5. If native chart conversion is unavailable, report the chart export as `BLOCKED` or switch to the confirmed native `PptxGenJS` chart route; do not silently deliver screenshot-only chart images.

### 3.4 Visual effects scope (decoration vs text-bearing)

`html_to_editable_pptx.js` runs `bg_capture` by default (`--bg-capture always`). It screenshots every **decoration node** into a slide-level bitmap and then removes it from the export tree, so any CSS effect on a decoration node ends up as pixels — not as a live PPTX shape. The dom-to-pptx blacklist applies **only to elements that survive capture**. ECharts/canvas chart nodes marked with `data-pptx-chart` and technical-diagram roots marked with `data-pptx-diagram` are not decoration nodes; the marked root and all descendants must stay out of the background screenshot path.

**Decoration nodes (free to use any visual effect):**
- Empty `<div>` (no text inside, no `<img>` inside)
- `<svg>`, `<hr>`, `<canvas>`
- Anything nested inside an `<svg>`

**Allowed on decoration nodes and on `.slide` background:**
`transform`, `clip-path`, `text-shadow`, `backdrop-filter`, `mix-blend-mode`, `animation`, `transition`, `radial-gradient`, `conic-gradient`, `filter: drop-shadow/brightness/contrast/saturate/hue-rotate/...`.

**Still forbidden everywhere (bg_capture does not fix these):**
- Viewport units `vh/vw/vmin/vmax` — these are layout sizes, not visual effects
- `<video>`, `<audio>`, `<iframe>` — not captured at all
- Non-absolute / non-data / non-file `<img>` src on text path
- `position: static` or `overflow: visible` on `.slide`

**Still forbidden on text-bearing elements** (these survive capture as live PPTX shapes):
- `transform`, `text-shadow`, `clip-path`, `backdrop-filter`, `mix-blend-mode`, `animation`, `transition`, `radial-gradient`, `conic-gradient`, non-blur `filter`

**Practical guidance:**
- Want a glowing pill, gradient orb, blurred halo, rotated badge? Put it in an empty `<div>` (or SVG), then place the text in a **separate** sibling element on top. The decoration goes into the bitmap; the text stays sharp and editable.
- `.slide`'s own `background` can be any gradient / image / blend — it ends up in the bitmap layer.
- If `html_self_check.js` flags a visual effect on a "text-bearing element", the fix is usually to split the element: one decoration sibling for the effect, one text element for the words.

### 3.5 Legacy/custom-HTML fragment drafting

This section is an escape route only when no registered layout can represent a
required page and the user accepts legacy free-form HTML. A normal controlled
deck writes `deck.json` and renders it with `render_deck_html.js`, regardless of
slide count. On the legacy route, a full multi-slide deck's HTML is large. Emitting it through a single
`write_file` call routinely exceeds the provider's output-token limit, so the
call is truncated mid-stream (`finish_reason=length`) and the whole turn is
lost. Avoid this by authoring the deck in fragments and merging them with a
script — the model never has to stream the entire deck in one tool call.

**Workflow:**

1. Copy `references/starter/common.css` to `drafts/common.css` — it already
   contains the locked `.slide` 1920×1080 frame. Add the deck's shared CSS
   (palette variables, typography, reusable component classes) into this same
   file **once**; do not edit the `.slide` width/height/position/overflow, and do
   not repeat styles inline on every slide — define a class in `common.css` and
   reference it.
2. Author each contiguous slide range into its own draft file, e.g.
   `drafts/slides_01_04.html`, `drafts/slides_05_08.html`,
   `drafts/slides_09_12.html`. Each draft contains **only**
   `<section class="slide" data-slide="NN">…</section>` blocks for its range —
   no `<html>`, `<head>`, `<body>`, `<style>`, or `<script>` wrapper.
   - **Every section MUST carry a numeric `data-slide`** (`merge_html_fragments.js`
     rejects any section without one). Number them **`01`, `02`, …` continuously
     from `01` across the whole deck** (not per-fragment) — the merge enforces a
     gap-free, non-duplicated `01..N` sequence and sorts by `data-slide`, so the
     fragment file order on the command line does not matter. Example:
     ```html
     <section class="slide" data-slide="05">…</section>
     <section class="slide" data-slide="06">…</section>
     ```
   - **Charts inside a fragment must use the `data-chart-spec` or
     `data-chart-spec-src` attribute form** (see §3.3) — the inline
     `<script type="application/json" data-chart-spec>` variant is **forbidden in
     fragments** because the merge strips/rejects any `<script>`. Put the spec in
     an attribute, or reference an external `assets/data/*.json` via
     `data-chart-spec-src`.
3. Keep each fragment small enough to write comfortably in one `write_file`
   call (roughly ≤4 slides per fragment, fewer if a slide is dense). When in
   doubt, split further.
4. Merge into the final single-file `deck.html`:

   ```bash
   ${BOX_AGENT_NODE:-node} "$PPTX_SKILL_DIR/scripts/merge_html_fragments.js" \
     --css drafts/common.css \
     --out deck.html \
     --title "Deck title" \
     drafts/slides_01_04.html drafts/slides_05_08.html drafts/slides_09_12.html
   ```

5. Continue with HTML self-check and export on the merged `deck.html` as usual.

**When sub-agents drafted the slides:** each sub-agent writes its own fragment
file directly (`drafts/slides_NN_MM.html`). The orchestrator then **only runs
the merge command** above. It must **never** read the drafts back and paste
their combined content into a single `write_file` — that recreates the exact
truncation failure this workflow exists to prevent.

## 4. QA Gates

For controlled HTML, `qa/outline_check.json`, `qa/deck_contract.json`,
`qa/deck_spec.json`, `qa/image_manifest.json`, `qa/html_self_check.json`, and
`qa/runtime_probe.json` must exist with top-level `"ok": true` before delivery.
The core deck-schema portion of deck spec, HTML self-check, and runtime probe
remain blocking quality gates. Exact outline title/message binding drift is a
degraded semantic warning when the same deck still passes the core schema.
Image-manifest failures are normalized into `ok: true`, `advisory: true` with
their original issues preserved as warnings so structurally renderable decks
still produce HTML; such output is a degraded delivery, not a clean completion.
`qa/truth_check.json` is a post-generation advisory: it should record available
source/URL/private-fact findings, but `"ok": false`, missing sources, or
unverified claims must not block `index.html`, trigger repair, or prevent the
workflow from completing. Preserve scaffolded `source_facts` and
`research_facts`, and summarize only user-impacting advisory findings under the
localized usage-note label from §6. For spec failures, patch only named paths; if the same issue
class returns twice, stop automatic repair and follow §0 rule 13. For self-check
**issues**, retry at most three focused repair rounds; if the issue set stops
shrinking, stop and report the HTML as a blocked draft rather than looping or
claiming completion. Warnings are diagnostic: inspect actual clipping/overflow
or unreadable contrast, but do not loop on font-metric slack warnings alone.
The self-check aggregates text slack into at most one summary warning per slide;
record only its concrete user impact under the localized usage-note label rather than treating every
text node as a separate defect. Keep raw warning counts internal. If required QA
reports contain warnings, the final delivery must not describe the run as
"clean", "all green", or warning-free, but it must mention a note only when the
finding changes how the user should preview, edit, download, or publicly use the
result.
If `assets/generated/manifest.json` contains `layout_contract`, `qa/image_layout_contract.json` must exist and pass before HTML self-check.

For every created or modified `.pptx`, additionally run package validation,
text extraction, placeholder scan, and slide count/order checks. These PPTX-only
checks do not apply when the requested/default deliverable is HTML alone.

Rendered visual inspection is **not** in the required list. See §4.2.

### 4.1 Visual issue triage

When rendered visual inspection surfaces a problem, classify it before reacting. Do **not** change route or strategy for cosmetic issues.

**Blocker — must fix:**
- Content extending outside the slide bounds, or a large overflow (>64px) that
  breaks the layout — self-check reports these as `issues`
- Image failed to load, broken asset references
- Wrong slide order, missing pages, misaligned page numbers
- Layout collapse (overlapping blocks, zero-size containers)
- Typos in user-supplied copy, factual errors
- dom-to-pptx drift that hides a whole element

**Cosmetic — accept and move on:**
- Minor text overflow within the authored slack (≤64px) — self-check reports
  these as `warnings`, not `issues`; they do not block export
- Watermark / signature artifacts on generated images
- A single line wrap on a long title or trailing punctuation
- Minor kerning / leading drift after dom-to-pptx export
- Color shifts within the same palette family
- Subpixel alignment between adjacent blocks

**Forbidden reactions to cosmetic issues:**
- Switching `generate` → `draw_in_html` / pure vector / icons
- Switching HTML-first → `PptxGenJS` or `python-pptx`
- Abandoning the image plan and rewriting slides text-only
- Cascading "re-check after fix" loops that surface new cosmetic nits

Cosmetic issues may be summarized under the localized usage-note label only when they can affect
how the user views or edits the deck. Purely diagnostic findings stay in the QA
reports and the host's collapsed process details. They do not block delivery,
do not justify a route switch, and do not get a repair attempt.

### 4.2 Visual inspection is optional

Rendered visual inspection (`scripts/render_pptx.py` + reading the resulting images) is **opt-in**, not a required gate.

**Default behavior:** skip rendered visual inspection. The controlled HTML QA
reports above are sufficient for an HTML-only delivery; PPTX structural QA is
sufficient for an exported `.pptx`. Do not call `render_pptx.py` for visual
judgment on every deck.

**Trigger visual inspection only when:**
1. The user explicitly asks to see / review / render the deck.
2. A blocker-class issue is already suspected from structural QA (e.g. text-extract shows truncated content) and visual confirmation is needed to locate the failure.

**When visual inspection runs:**
1. One pass only. Classify findings per §4.1.
2. Fix blockers, accept cosmetics, report.
3. Do **not** re-render after the fix to verify cosmetics. Re-render only if the fix targeted a blocker.
4. Do **not** trigger a second visual pass to "double-check" your own judgment.

Rendering for the user's own preview (so they can open the PNGs) is fine and does not count as visual QA — just generate the images, do not narrate findings or self-critique.

## 5. Office Raccoon Runtime

Default controlled HTML authoring needs no export preflight. Only when an
explicit PPTX export or a runtime/dependency failure makes these details
relevant, read in this order:

1. `references/runtime-office-raccoon.md`
2. `references/dependency-policy.md`
3. `references/shell-safety.md`

Use managed variables for all commands:

1. `$BOX_AGENT_NODE`, `$BOX_AGENT_PYTHON`, `$BOX_AGENT_NPM`
2. `$BOX_AGENT_RENDER_RUNTIME`, `$BOX_AGENT_SOFFICE`, `$BOX_AGENT_PDFTOPPM`
3. `$BOX_AGENT_RUNTIME_PREFIX`

Install only into managed Office Raccoon prefixes.
No global, Homebrew, or system-wide installs without explicit approval.
No `/tmp`, no `>/tmp`, and no writes outside the canonical delivery root
selected by the runtime. Never add another nested `output/` directory.

## 6. Final Response Format

The normal user-facing response must be concise and use the active user-visible
response language. An explicit user language request wins; otherwise follow the
host `ui_language` instruction when present, then the language of the user's
task. Never override that contract with a locale-specific default, and do not
mix headings from one language with body text from another.

Present information in this priority order:

1. Start with one plain-language completion sentence using the matching
   localized completion, editable-draft, or incomplete status below.
2. Name only the primary user deliverable: the editable presentation, requested
   PowerPoint export, or requested PDF export. Do not enumerate reproducibility
   or workflow files.
3. Add the localized usage-note label only when a finding changes how the user should preview,
   edit, download, or publicly use the result. Explain the concrete impact and
   recommended action; omit purely diagnostic findings.
4. End with concrete next steps in the active response language, for example
   previewing, downloading, continuing edits, or updating after adding sources.
5. Add a compact block using the matching localized generation-details heading;
   the host
   moves this section into its collapsed details area before the existing
   generated-files module. Retain QA totals and relevant technical artifacts.
   Translate fixed status concepts into the matching localized QA labels. Keep artifact
   names and directories such as `deck.patch.json`, `qa/`, and `research/`
   literal so the result remains traceable.

Use these stable labels for the host-supported locales. If the user explicitly
requests another language, translate the same semantic roles consistently:

| Role | `zh` | `en` | `ja` |
| --- | --- | --- | --- |
| Complete | `演示文稿已完成` | `Presentation complete` | `プレゼンテーションが完成しました` |
| Editable draft | `已生成可编辑草稿` | `Editable draft ready` | `編集可能な下書きができました` |
| Incomplete | `演示文稿尚未完成` | `Presentation not complete` | `プレゼンテーションは未完成です` |
| Usage note | `使用前注意` | `Before use` | `ご利用前の注意` |
| Generation details heading | `## 生成详情` | `## Generation details` | `## 生成の詳細` |
| QA passed label | `质量检查` | `Quality checks` | `品質チェック` |
| QA notice label | `检查提示` | `Check notices` | `確認事項` |

Never print raw status key names such as `qa_ok` or `qa_warnings`, internal
research delivery-mode ids such as `framework` or `partial`, validator names,
or commands. Their values may be retained only under clear localized labels.
Technical filenames and report directories remain literal under the localized
generation-details heading.
Do not turn a diagnostic count into a user warning: explain it under
the localized usage-note section only when it changes how the user should use the result.

Translate internal outcomes into user impact:

- When the editable HTML is current and core checks pass, say it can be
  previewed and edited normally.
- When source or private-fact advisories exist, make clear that they do not
  prevent preview/editing and state whether public sharing needs source review.
- When a usable HTML draft exists but a presentation issue may affect display,
  use the localized editable-draft status and name the affected use, not the validator.
- When HTML is usable but a requested PPTX/PDF export is unavailable, deliver
  the presentation and say only that the requested format has not been exported.
- When no trustworthy HTML exists, do not claim completion.

If a blocking structural, HTML, runtime, explicitly required image, or export
step is blocked, explain the user-visible consequence in the active response
language. Never treat a
source/URL/private-fact advisory as a blocked presentation.

## 7. References

1. `references/outline.md`
2. `references/html-first.md`
3. `references/html-editable.md`
4. `references/pptxgenjs.md`
5. `references/ooxml-editing.md`
6. `references/qa.md`
7. `references/api-integration.md`
8. `references/runtime-office-raccoon.md`
9. `references/dependency-policy.md`
10. `references/shell-safety.md`
11. `references/image-assets.md`

## 8. Mode lock and fallback

1. Lock the default controlled HTML route immediately. Preflight only an
   explicit PPTX export; ask for confirmation only when a material route
   change needs user choice.
2. Do not switch from HTML-first to `PptxGenJS` to speed up completion.
3. Do not switch to `python-pptx` for new deck creation.
4. If preflight or host checks change while running, restart from current source with the new route decision.
5. Keep report language explicit: `export blocked`, `render blocked`, `dependency blocked`, `mode locked`.

## 9. Compatibility baseline

1. Support macOS, Linux, and Windows for this skill.
2. Use managed runtime binaries first, then fallback checks.
3. Keep generated files inside workspace or requested output folder.
4. Prefer editable `index.html + deck.json + assets/` delivery. Treat PPTX, PDF, portable single-file HTML, and packaged archives as explicit exports.
5. Keep output deterministic for reruns.
